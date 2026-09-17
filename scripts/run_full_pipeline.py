"""End-to-end I07 pipeline run and review report.

    staging -> features -> forecasting -> inventory -> OAR -> recommendations

Calls the existing, unmodified phase services and then *reports* what they
wrote, as fixed-width tables meant to be read in a terminal. It answers one
question directly -- **which material-plants end up with a numerical Safety
Stock, ROP and Max Stock, and what blocks the rest** -- and shows the Phase 4
backtest evidence behind the champion model on the way.

This script contains no business logic. Every number below is read back from a
table some phase wrote; nothing here computes, defaults, rounds or reconciles
a value, so a figure that looks wrong is a finding about the pipeline and not
about this file.

Policy comes from the same ``dev_fixtures.default_policy()`` fallback every
phase uses, so the report runs under whatever ``I7_DEV_MOCK_*`` flags the
environment sets -- and prints them first, because the same catalogue produces
very different coverage under a mocked policy than under an unsigned one.

Usage::

    python -m scripts.run_full_pipeline                 # run everything, then report
    python -m scripts.run_full_pipeline --report-only   # report the latest runs only
    python -m scripts.run_full_pipeline --skip staging  # reuse what is already staged
    python -m scripts.run_full_pipeline --rows 50       # more detail rows per table
    python -m scripts.run_full_pipeline --rows 0        # every row
    python -m scripts.run_full_pipeline --material 4000000123
    python -m scripts.run_full_pipeline --blocked-only  # only what failed to produce numbers

Requires a working DATABASE_URL (Azure SQL). Queries are built with SQLAlchemy
constructs rather than raw SQL so row limits compile to the dialect's own
syntax.
"""

import argparse
import os
import sys
from decimal import Decimal

from sqlalchemy import case, func, select

from app.core.db import get_sessionmaker
from app.models.i7_features import MaterialFeature
from app.models.i7_forecast import Forecast
from app.models.i7_inventory import InventoryCalculation
from app.models.i7_oar import OarNeighbour, OarTargetResult
from app.models.i7_recommendation import Recommendation

PHASES = ("staging", "features", "forecasting", "inventory", "oar", "recommendations")


# --- terminal tables --------------------------------------------------------


def rule(char: str = "=", width: int = 100) -> str:
    return char * width


def heading(title: str) -> None:
    print(f"\n{rule()}\n{title}\n{rule()}")


def cell(value) -> str:
    """One value as a display string. ``None`` is shown as ``-`` rather than
    blank, so a missing number is visibly missing instead of looking like a
    column that ran out of data."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Decimal):
        # Trailing zeros carry no information here and cost column width.
        normalised = value.normalize()
        return f"{normalised:f}"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def table(headers: list[str], rows: list[tuple], *, max_width: int = 38) -> None:
    """Fixed-width table. Numeric-looking columns right-align."""
    if not rows:
        print("  (no rows)")
        return

    body = [[cell(value) for value in row] for row in rows]
    for line in body:
        for index, text in enumerate(line):
            if len(text) > max_width:
                line[index] = text[: max_width - 1] + "…"

    widths = [
        max(len(headers[index]), *(len(line[index]) for line in body))
        for index in range(len(headers))
    ]

    def numeric(index: int) -> bool:
        return all(
            line[index] == "-" or line[index].lstrip("-").replace(".", "", 1).isdigit()
            for line in body
        )

    aligns = [">" if numeric(index) else "<" for index in range(len(headers))]
    last = len(headers) - 1

    def render(values: list[str], pad: str) -> str:
        # The final column is never padded. Padding it adds trailing spaces
        # that push the line past the terminal width and make it wrap, which
        # is what turns a readable table into a ragged one.
        parts = [
            values[index] if index == last else f"{values[index]:{pad[index]}{widths[index]}}"
            for index in range(len(headers))
        ]
        return "  " + "  ".join(parts)

    print(render(list(headers), "<" * len(headers)))
    print(render(["-" * width for width in widths], "<" * len(headers)))
    for line in body:
        print(render(line, aligns))


def counts_table(title: str, counts: dict) -> None:
    print(f"\n{title}")
    if not counts:
        print("  (none)")
        return
    total = sum(counts.values()) or 1
    table(
        ["value", "count", "share"],
        [
            (key, value, f"{100 * value / total:.1f}%")
            for key, value in sorted(counts.items(), key=lambda item: -item[1])
        ],
    )


def truncated_note(shown: int, total: int, limit: int) -> None:
    if limit and total > shown:
        print(f"  ... {total - shown} more row(s) not shown (--rows {total} for all)")


# --- policy -----------------------------------------------------------------


def report_policy() -> object:
    from app.initiatives.i7.policy.dev_fixtures import default_policy

    policy = default_policy()

    heading("ENVIRONMENT AND POLICY")
    table(
        ["setting", "value"],
        [
            ("I7_DEV_MOCK_SERVICE_LEVEL", os.environ.get("I7_DEV_MOCK_SERVICE_LEVEL", "(unset)")),
            ("I7_DEV_MOCK_MAX_STOCK", os.environ.get("I7_DEV_MOCK_MAX_STOCK", "(unset)")),
            ("policy", f"{policy.policy_id} v{policy.policy_version}"),
            ("policy status", policy.status.value),
            ("service level configured", policy.service_level.is_configured),
            ("max stock strategy", policy.max_stock.strategy or "(not configured)"),
            ("min similarity", policy.similarity.minimum_similarity),
            ("min neighbours", policy.similarity.minimum_neighbours),
            ("max neighbours", policy.similarity.maximum_neighbours),
        ],
    )

    unresolved = policy.unresolved_policies()
    print("\nUnresolved business policies (awaiting Vedanta)")
    if unresolved:
        for name in unresolved:
            print(f"  - {name}")
    else:
        print("  (none)")

    if not policy.service_level.is_configured or not policy.max_stock.is_configured:
        print(
            "\n  NOTE: at least one business policy is unresolved, so parameters will\n"
            "  correctly block rather than produce numbers. For a DEV/TEST run with\n"
            "  numerical output, set I7_DEV_MOCK_SERVICE_LEVEL=true and\n"
            "  I7_DEV_MOCK_MAX_STOCK=true (see docs/i07_dev_numerical_values.md)."
        )
    return policy


# --- phases -----------------------------------------------------------------


def run_phases(skip: set[str], force: bool) -> dict:
    """Run each phase in order, reporting what it wrote. A failing phase stops
    the run: every later phase reads the earlier one's output, so continuing
    would report a stale run as if it were this one's."""
    results: dict[str, object] = {}

    if "staging" not in skip:
        heading("PHASE 2 - STAGING")
        from app.initiatives.i7.adapters.extract import stage_extract

        result = stage_extract()
        results["staging"] = result
        table(
            ["metric", "value"],
            [
                ("run_id", result.run_id),
                ("status", result.status),
                ("materials", result.materials),
                ("material-plants", result.material_plants),
                ("stock rows", result.stock),
                ("consumption rows", result.consumption),
                ("purchase orders", result.purchase_orders),
            ],
        )
        counts_table("Rejections", result.rejections)
        if result.error:
            print(f"\n  ERROR: {result.error}")
            return results

    if "features" not in skip:
        heading("PHASE 3 - FEATURE STORE")
        from app.initiatives.i7.features.builder import build_features

        result = build_features()
        results["features"] = result
        table(
            ["metric", "value"],
            [("run_id", result.run_id), ("status", result.status), ("features", result.features)],
        )
        counts_table("History status", result.history_status_counts)
        counts_table("Demand class", result.demand_class_counts)
        counts_table("OAR scope", result.oar_scope_counts)
        if result.error:
            print(f"\n  ERROR: {result.error}")
            return results

    if "forecasting" not in skip:
        heading("PHASE 4 - FORECASTING AND BACKTESTING")
        from app.initiatives.i7.forecasting.service import run_forecasting

        print("  Running SES/Auto-ARIMA, SBA/LightGBM, TSB with fresh backtests ...")
        result = run_forecasting()
        results["forecasting"] = result
        table(
            ["metric", "value"],
            [
                ("run_id", result.run_id),
                ("status", result.status),
                ("materials evaluated", result.materials_evaluated),
                ("forecast rows written", result.forecasts_written),
            ],
        )
        counts_table("Model status", result.model_status_counts)
        counts_table("Backtest status", result.backtest_status_counts)
        counts_table("Lead-time source", result.lead_time_source_counts)
        if result.error:
            print(f"\n  ERROR: {result.error}")
            return results

    if "inventory" not in skip:
        heading("PHASE 5 - INVENTORY CALCULATIONS")
        from app.initiatives.i7.inventory.service import run_inventory_calculations

        result = run_inventory_calculations(force=force)
        results["inventory"] = result
        table(
            ["metric", "value"],
            [
                ("run_id", result.run_id),
                ("status", result.status),
                ("calculations", result.calculations),
                ("reused existing run", result.reused_existing),
            ],
        )
        counts_table("Safety stock status", result.safety_stock_status_counts)
        counts_table("ROP status", result.rop_status_counts)
        counts_table("Max stock status", result.max_stock_status_counts)
        counts_table("Lead-time method", result.lead_time_method_counts)
        if result.error:
            print(f"\n  ERROR: {result.error}")
            return results

    if "oar" not in skip:
        heading("PHASE 6 - OAR SIMILARITY")
        from app.initiatives.i7.oar.service import run_oar_similarity

        result = run_oar_similarity(force=force)
        results["oar"] = result
        table(
            ["metric", "value"],
            [
                ("run_id", result.run_id),
                ("status", result.status),
                ("targets evaluated", result.targets_evaluated),
                ("reused existing run", result.reused_existing),
                ("text similarity available", result.text_similarity_available),
            ],
        )
        counts_table("Confidence", result.confidence_counts)
        counts_table("Estimate status", result.estimate_status_counts)
        counts_table("Neighbour count distribution", result.neighbour_count_distribution)
        if result.error:
            print(f"\n  ERROR: {result.error}")
            return results

    if "recommendations" not in skip:
        heading("PHASE 7 - RECOMMENDATIONS")
        from app.initiatives.i7.recommendations.service import generate_recommendations

        result = generate_recommendations()
        results["recommendations"] = result
        table(
            ["metric", "value"],
            [
                ("status", result.status),
                ("written", result.recommendations_written),
                ("reused existing", result.reused_existing),
            ],
        )
        counts_table("Lifecycle status", result.status_counts)
        if result.error:
            print(f"\n  ERROR: {result.error}")

    return results


# --- reports ----------------------------------------------------------------


def report_backtests(session, limit: int, material: str | None) -> None:
    heading("PHASE 4 DETAIL - MODEL PARAMETERS AND BACKTEST METRICS")

    run_id = session.execute(select(func.max(Forecast.forecast_run_id))).scalar()
    if run_id is None:
        print("  (no forecast run)")
        return
    print(f"\nforecast_run_id = {run_id}")

    statement = select(Forecast).where(Forecast.forecast_run_id == run_id)
    if material:
        statement = statement.where(Forecast.sap_material_number == material)
    total = session.execute(
        select(func.count()).select_from(statement.subquery())
    ).scalar()

    statement = statement.order_by(
        Forecast.sap_material_number, Forecast.sap_plant_code, Forecast.model_name
    )
    if limit:
        statement = statement.limit(limit)
    rows = session.execute(statement).scalars().all()

    print(f"\nPer material-plant, per model ({total} row(s))")
    table(
        [
            "material", "plant", "class", "model", "champ", "fc status", "rate",
            "backtest", "origins", "pinball", "bias %", "MAE", "fill rate", "parameters",
        ],
        [
            (
                row.sap_material_number, row.sap_plant_code, row.demand_class,
                row.model_name, row.is_champion, row.forecast_status, row.forecast_rate,
                row.backtest_status,
                f"{row.origins_evaluated or 0}/{row.required_origins or 0}",
                row.pinball_loss, row.bias_percentage, row.mean_absolute_error,
                row.fill_rate, row.parameters,
            )
            for row in rows
        ],
    )
    truncated_note(len(rows), total, limit)

    champions = dict(
        session.execute(
            select(Forecast.model_name, func.count())
            .where(Forecast.forecast_run_id == run_id, Forecast.is_champion.is_(True))
            .group_by(Forecast.model_name)
        ).all()
    )
    counts_table("Champion model selected", champions)


def report_inventory(session, limit: int, material: str | None, blocked_only: bool) -> None:
    heading("PHASE 5 DETAIL - SAFETY STOCK / ROP / MAX STOCK")

    run_id = session.execute(
        select(func.max(InventoryCalculation.inventory_run_id))
    ).scalar()
    if run_id is None:
        print("  (no inventory run)")
        return
    print(f"\ninventory_run_id = {run_id}")

    statement = select(InventoryCalculation).where(
        InventoryCalculation.inventory_run_id == run_id
    )
    if material:
        statement = statement.where(InventoryCalculation.sap_material_number == material)
    if blocked_only:
        statement = statement.where(InventoryCalculation.safety_stock.is_(None))

    total = session.execute(select(func.count()).select_from(statement.subquery())).scalar()
    ordered = statement.order_by(
        InventoryCalculation.sap_material_number, InventoryCalculation.sap_plant_code
    )
    if limit:
        ordered = ordered.limit(limit)
    rows = session.execute(ordered).scalars().all()

    print(f"\n{total} row(s)")
    table(
        [
            "material", "plant", "class", "crit", "svc lvl", "Z", "LT(m)",
            "SS status", "SS", "ROP status", "ROP", "Max status", "Max", "strategy",
        ],
        [
            (
                row.sap_material_number, row.sap_plant_code, row.demand_class,
                row.criticality, row.service_level, row.z_factor, row.lt_avg_months,
                row.safety_stock_status, row.safety_stock,
                row.rop_status, row.rop,
                row.max_stock_status, row.max_stock, row.max_stock_strategy,
            )
            for row in rows
        ],
    )
    truncated_note(len(rows), total, limit)

    complete = session.execute(
        select(func.count())
        .select_from(InventoryCalculation)
        .where(
            InventoryCalculation.inventory_run_id == run_id,
            InventoryCalculation.safety_stock.is_not(None),
            InventoryCalculation.rop.is_not(None),
            InventoryCalculation.max_stock.is_not(None),
        )
    ).scalar()
    calculated = session.execute(
        select(func.count())
        .select_from(InventoryCalculation)
        .where(InventoryCalculation.inventory_run_id == run_id)
    ).scalar()
    if calculated:
        print(
            f"\nDonor-eligible (all three values present): {complete} of {calculated} "
            f"({100 * complete / calculated:.1f}%)"
        )
    else:
        print("\n(no calculations)")


def report_oar(session, limit: int, material: str | None) -> None:
    heading("PHASE 6 DETAIL - OAR TARGETS AND NEIGHBOURS")

    run_id = session.execute(select(func.max(OarTargetResult.oar_run_id))).scalar()
    if run_id is None:
        print("  (no OAR run)")
        return
    print(f"\noar_run_id = {run_id}")

    statement = select(OarTargetResult).where(OarTargetResult.oar_run_id == run_id)
    if material:
        statement = statement.where(OarTargetResult.sap_material_number == material)
    total = session.execute(select(func.count()).select_from(statement.subquery())).scalar()

    ordered = statement.order_by(
        OarTargetResult.estimate_status,
        OarTargetResult.sap_material_number,
        OarTargetResult.sap_plant_code,
    )
    if limit:
        ordered = ordered.limit(limit)
    rows = session.execute(ordered).scalars().all()

    print(f"\n{total} target(s)")
    table(
        [
            "material", "plant", "status", "conf", "cands", "eligible",
            "neighbours", "best sim", "estimate status", "SS", "ROP", "Max",
        ],
        [
            (
                row.sap_material_number, row.sap_plant_code, row.status, row.confidence,
                row.candidates_considered, row.eligible_candidates, row.neighbour_count,
                row.best_similarity, row.estimate_status,
                row.safety_stock, row.rop, row.max_stock,
            )
            for row in rows
        ],
    )
    truncated_note(len(rows), total, limit)

    if material:
        print("\nNeighbours borrowed from")
        neighbours = session.execute(
            select(OarNeighbour)
            .where(
                OarNeighbour.oar_run_id == run_id,
                OarNeighbour.sap_material_number == material,
            )
            .order_by(OarNeighbour.sap_plant_code, OarNeighbour.rank)
        ).scalars().all()
        table(
            [
                "target plant", "rank", "neighbour", "plant", "similarity",
                "struct", "text", "business", "eligible", "SS", "ROP", "Max",
            ],
            [
                (
                    row.sap_plant_code, row.rank, row.neighbour_material, row.neighbour_plant,
                    row.combined_similarity, row.structured_similarity, row.text_similarity,
                    row.business_similarity, row.inventory_eligible,
                    row.safety_stock, row.rop, row.max_stock,
                )
                for row in neighbours
            ],
        )


def report_recommendations(session, limit: int, material: str | None, blocked_only: bool) -> None:
    heading("PHASE 7 DETAIL - RECOMMENDED VALUES PER MATERIAL")

    feature_run_id = session.execute(select(func.max(Recommendation.feature_run_id))).scalar()
    if feature_run_id is None:
        print("  (no recommendations)")
        return
    print(f"\nfeature_run_id = {feature_run_id}")

    scope = Recommendation.feature_run_id == feature_run_id

    total = session.execute(
        select(func.count()).select_from(Recommendation).where(scope)
    ).scalar()
    complete = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            scope,
            Recommendation.recommended_safety_stock.is_not(None),
            Recommendation.recommended_rop.is_not(None),
            Recommendation.recommended_max_stock.is_not(None),
        )
    ).scalar()

    coverage = []
    for label, column in (
        ("safety stock", Recommendation.recommended_safety_stock),
        ("ROP", Recommendation.recommended_rop),
        ("max stock", Recommendation.recommended_max_stock),
    ):
        present = session.execute(
            select(func.count()).select_from(Recommendation).where(scope, column.is_not(None))
        ).scalar()
        share = f"{100 * present / total:.1f}%" if total else "-"
        coverage.append((label, present, total, share))

    print("\nCoverage - material-plants with a numerical recommended value")
    table(["parameter", "with a value", "of total", "share"], coverage)
    print(
        f"\n  ALL THREE present: {complete} of {total}"
        + (f" ({100 * complete / total:.1f}%)" if total else "")
    )

    by_path = session.execute(
        select(
            Recommendation.is_oar,
            Recommendation.status,
            func.count(),
        )
        .where(scope)
        .group_by(Recommendation.is_oar, Recommendation.status)
    ).all()
    print("\nBy path and lifecycle status")
    table(
        ["path", "status", "count"],
        [
            ("OAR" if is_oar else "NORMAL", status, count)
            for is_oar, status, count in sorted(by_path, key=lambda r: (-r[2],))
        ],
    )

    blocked = session.execute(
        select(Recommendation.blocking_reason, func.count())
        .where(scope, Recommendation.recommended_safety_stock.is_(None))
        .group_by(Recommendation.blocking_reason)
    ).all()
    print("\nWhy the rest have no numbers")
    table(
        ["blocking reason", "count"],
        sorted(((reason, count) for reason, count in blocked), key=lambda r: -r[1]),
    )

    statement = select(Recommendation).where(scope)
    if material:
        statement = statement.where(Recommendation.sap_material_number == material)
    if blocked_only:
        statement = statement.where(Recommendation.recommended_safety_stock.is_(None))
    shown_total = session.execute(
        select(func.count()).select_from(statement.subquery())
    ).scalar()

    # CASE rather than ordering on ``is_(None)`` directly: SQL Server has no
    # boolean type and rejects a predicate in ORDER BY, so the expression form
    # that works on Postgres would fail on the actual target database.
    ordered = statement.order_by(
        case((Recommendation.recommended_safety_stock.is_(None), 1), else_=0),
        Recommendation.sap_material_number,
        Recommendation.sap_plant_code,
    )
    if limit:
        ordered = ordered.limit(limit)
    rows = session.execute(ordered).scalars().all()

    print(f"\nRecommendations ({shown_total} row(s))")
    table(
        [
            "material", "plant", "path", "class", "conf", "status",
            "cur SS", "rec SS", "cur ROP", "rec ROP", "cur Max", "rec Max", "blocked by",
        ],
        [
            (
                row.sap_material_number, row.sap_plant_code,
                "OAR" if row.is_oar else "NORMAL", row.demand_class, row.confidence,
                row.status,
                row.current_safety_stock, row.recommended_safety_stock,
                row.current_rop, row.recommended_rop,
                row.current_max_stock, row.recommended_max_stock,
                row.blocking_reason,
            )
            for row in rows
        ],
    )
    truncated_note(len(rows), shown_total, limit)


def report_catalogue(session) -> None:
    heading("CATALOGUE")
    total = session.execute(select(func.count()).select_from(MaterialFeature)).scalar()
    by_history = dict(
        session.execute(
            select(MaterialFeature.history_status, func.count()).group_by(
                MaterialFeature.history_status
            )
        ).all()
    )
    by_class = dict(
        session.execute(
            select(MaterialFeature.demand_class, func.count()).group_by(
                MaterialFeature.demand_class
            )
        ).all()
    )
    print(f"\nMaterial-plants in the feature store: {total}")
    counts_table("History status", by_history)
    counts_table("Demand class", by_class)


# --- entry point ------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the I07 pipeline end to end and report the results.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="do not run any phase; report the latest existing runs",
    )
    parser.add_argument(
        "--skip",
        default="",
        help=f"comma-separated phases to skip. One or more of: {', '.join(PHASES)}",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="recompute the idempotent phases (inventory, OAR) instead of reusing a run",
    )
    parser.add_argument(
        "--rows", type=int, default=25, help="detail rows per table; 0 for all (default 25)"
    )
    parser.add_argument("--material", help="restrict the detail tables to one material number")
    parser.add_argument(
        "--blocked-only",
        action="store_true",
        help="show only material-plants that produced no numbers",
    )
    arguments = parser.parse_args()

    skip = {name.strip() for name in arguments.skip.split(",") if name.strip()}
    unknown = skip - set(PHASES)
    if unknown:
        parser.error(f"unknown phase(s): {', '.join(sorted(unknown))}")

    try:
        report_policy()

        if not arguments.report_only:
            run_phases(skip, arguments.force)
        else:
            print("\n(--report-only: no phase was run)")

        with get_sessionmaker()() as session:
            report_catalogue(session)
            report_backtests(session, arguments.rows, arguments.material)
            report_inventory(session, arguments.rows, arguments.material, arguments.blocked_only)
            report_oar(session, arguments.rows, arguments.material)
            report_recommendations(
                session, arguments.rows, arguments.material, arguments.blocked_only
            )

        heading("DONE")
        return 0

    except Exception as error:  # noqa: BLE001 - a report script reports its own failure
        sys.stdout.flush()
        print(f"\n{rule('!')}\n{type(error).__name__}: {error}\n{rule('!')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
